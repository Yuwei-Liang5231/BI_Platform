"""统一响应契约测试（D11：HTTP 码与 body code 对齐）。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.core.response import (
    HTTP_STATUS_BY_CODE,
    ApiResponse,
    BodyCode,
    BusinessError,
    fail_response,
    ok_response,
)


def test_api_response_ok_structure():
    resp = ApiResponse.ok({"a": 1})
    assert resp.code == 0
    assert resp.message == "ok"
    assert resp.data == {"a": 1}


def test_api_response_fail_structure():
    resp = ApiResponse.fail(40000, "参数错误")
    assert resp.code == 40000
    assert resp.message == "参数错误"
    assert resp.data is None


def test_http_status_mapping_alignment():
    """每个非成功业务码都能映射到 HTTP 状态码，且前三位对齐。"""
    for code, http_status in HTTP_STATUS_BY_CODE.items():
        if code == BodyCode.OK:
            assert http_status == 200
            continue
        assert str(code)[:3] == str(http_status)[:3], f"body code {code} 与 HTTP {http_status} 未对齐"


def test_ok_response_is_http_200_code_0():
    resp = ok_response({"x": 2})
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    assert resp.body is not None


def test_fail_response_http_alignment():
    assert fail_response(BodyCode.UNAUTHORIZED, "未登录").status_code == 401
    assert fail_response(BodyCode.FORBIDDEN, "无权限").status_code == 403
    assert fail_response(BodyCode.BAD_REQUEST, "参数错").status_code == 400
    assert fail_response(BodyCode.NOT_FOUND, "不存在").status_code == 404
    assert fail_response(BodyCode.CONFLICT, "冲突").status_code == 409


def test_fail_response_unknown_code_falls_back_to_500():
    resp = fail_response(99999, "未知码")
    assert resp.status_code == 500


def test_business_error_defaults():
    err = BusinessError("业务错误")
    assert err.code == BodyCode.BAD_REQUEST
    assert err.message == "业务错误"
