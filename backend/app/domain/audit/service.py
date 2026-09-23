"""操作审计日志服务（C2）：HTTP 中间件按「方法 + 路径白名单」自动记录写操作。

设计要点：
- **零埋点**：不侵入任何业务路由，中间件按白名单匹配（防漏记；新写接口只需
  在 ``_AUDIT_RULES`` 加一行）；
- **只记成功操作 + 登录成败**：2xx 记录（4xx 参数错误噪音大不记；登录例外，
  失败也是安全审计信号）；
- **响应后台任务落库**：审计 INSERT 挂在 ``response.background``（响应发送
  完成后执行，此时请求的 DB 会话已 teardown 提交/关闭）——规避 SQLite 主
  会话写锁导致的「database is locked」静默丢失（2026-09-23 B2 观测同因教训）；
- **username 冗余存储**：用户被删后审计仍可读；JWT 解析失败（匿名）记 NULL；
- **detail 轻量摘要**：仅 JSON body 的标量字段（截断），敏感键脱敏，
  multipart（文件上传）不解析 body。
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import Request, Response
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import get_logger

logger = get_logger("app.domain.audit.service")

# ---------------------------------------------------------------- 白名单
# (method, path 正则[相对 api_prefix，含命名组 rid 则记为 resource_id], action, resource_type)
_AUDIT_RULES_SRC: list[tuple[str, str, str, str]] = [
    # 认证与用户管理
    ("POST", r"^/auth/login$", "user.login", "user"),
    ("POST", r"^/auth/users$", "user.create", "user"),
    ("PATCH", r"^/auth/users/(?P<rid>\d+)$", "user.update", "user"),
    ("PUT", r"^/auth/users/(?P<rid>\d+)/restrictions$", "user.restrictions", "user"),
    # 数据集
    ("POST", r"^/datasets/upload$", "dataset.upload", "dataset"),
    ("PATCH", r"^/datasets/(?P<rid>\d+)/name$", "dataset.rename", "dataset"),
    ("PUT", r"^/datasets/(?P<rid>\d+)/semantic-annotations$", "dataset.annotations", "dataset"),
    ("POST", r"^/datasets/(?P<rid>\d+)/data$", "dataset.import", "dataset"),
    ("DELETE", r"^/datasets/(?P<rid>\d+)$", "dataset.delete", "dataset"),
    ("POST", r"^/datasets/batch-delete$", "dataset.batch_delete", "dataset"),
    ("POST", r"^/datasets/(?P<rid>\d+)/relations$", "relation.create", "relation"),
    ("DELETE", r"^/datasets/(?P<rid>\d+)/relations/(?P<rid2>\d+)$", "relation.delete", "relation"),
    # 指标
    ("POST", r"^/metrics$", "metric.create", "metric"),
    ("PATCH", r"^/metrics/(?P<rid>\d+)$", "metric.update", "metric"),
    ("DELETE", r"^/metrics/(?P<rid>\d+)$", "metric.delete", "metric"),
    ("POST", r"^/metrics/batch-status$", "metric.batch_status", "metric"),
    ("POST", r"^/metrics/batch-delete$", "metric.batch_delete", "metric"),
    ("PUT", r"^/metrics/(?P<rid>\d+)/anomaly-config$", "metric.anomaly_config", "metric"),
    # 项目
    ("POST", r"^/projects$", "project.create", "project"),
    ("PATCH", r"^/projects/(?P<rid>\d+)$", "project.update", "project"),
    ("DELETE", r"^/projects/(?P<rid>\d+)$", "project.delete", "project"),
    # 建模向导（模板导入）
    ("POST", r"^/templates/import$", "dataset.template_import", "dataset"),
    # 报告
    ("POST", r"^/reports/templates$", "report.template_create", "report_template"),
    ("PUT", r"^/reports/templates/(?P<rid>\d+)$", "report.template_update", "report_template"),
    ("DELETE", r"^/reports/templates/(?P<rid>\d+)$", "report.template_delete", "report_template"),
    ("POST", r"^/reports/generate$", "report.generate", "report"),
    ("POST", r"^/reports/instances/(?P<rid>\d+)/regenerate$", "report.regenerate", "report"),
    # LLM 模型管理
    ("POST", r"^/llm/models$", "llm.model_create", "llm_model"),
    ("PUT", r"^/llm/models/(?P<rid>\d+)$", "llm.model_update", "llm_model"),
    ("DELETE", r"^/llm/models/(?P<rid>\d+)$", "llm.model_delete", "llm_model"),
    ("POST", r"^/llm/models/(?P<rid>\d+)/activate$", "llm.model_activate", "llm_model"),
    # AI 运维
    ("POST", r"^/ai/insight/run$", "insight.run", "insight"),
]

# 路径正则预编译（定义处为源字符串，便于阅读）
_AUDIT_RULES = [(m, re.compile(p), a, t) for m, p, a, t in _AUDIT_RULES_SRC]

_SENSITIVE_KEYS = {"password", "api_key", "apikey", "token", "secret", "authorization"}
_DETAIL_MAX_LEN = 2000
_VALUE_MAX_LEN = 200


def match_audit_rule(method: str, path: str) -> tuple[str, str, str | None] | None:
    """按 (method, path) 匹配白名单 → (action, resource_type, resource_id)。"""
    for m, pattern, action, rtype in _AUDIT_RULES:
        if m != method:
            continue
        mt = pattern.match(path)
        if mt:
            rid = mt.groupdict().get("rid") or mt.groupdict().get("rid2")
            return action, rtype, rid
    return None


def _summarize_body(body: bytes | None) -> dict | None:
    """JSON body 轻量摘要：标量字段（截断）+ 敏感键脱敏；解析失败返回 None。"""
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # multipart（文件上传）等非 JSON body 不摘要
    if not isinstance(obj, dict):
        return None

    def _clip(v: Any) -> Any:
        if isinstance(v, str):
            return v[:_VALUE_MAX_LEN]
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        if isinstance(v, list) and len(v) <= 50 and all(isinstance(i, (int, str)) for i in v):
            return [str(i)[:60] for i in v]
        return None  # 嵌套结构（calc_rule 等）不入审计摘要

    out: dict = {}
    for k, v in obj.items():
        if k.lower() in _SENSITIVE_KEYS:
            out[k] = "***"
            continue
        clipped = _clip(v)
        if clipped is not None:
            out[k] = clipped
    return out


def write_audit_log(entry: dict) -> None:
    """落一条审计日志（尽力而为，独立短会话；失败只记日志零阻塞）。"""
    try:
        from app.infra.database import fresh_session
        from app.infra.models import AuditLog, User

        user_id = entry.get("user_id")
        username = entry.get("username")
        session = fresh_session()
        try:
            if user_id is not None and not username:
                u = session.get(User, int(user_id))
                username = u.username if u else None
            row = AuditLog(
                user_id=user_id,
                username=username,
                action=(entry.get("action") or "unknown")[:60],
                resource_type=entry.get("resource_type"),
                resource_id=entry.get("resource_id"),
                method=entry.get("method", ""),
                path=(entry.get("path") or "")[:200],
                status_code=entry.get("status_code", 200),
                detail_json=json.dumps(entry.get("detail"), ensure_ascii=False) if entry.get("detail") else None,
            )
            session.add(row)
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 审计零阻塞：失败只记日志
        logger.warning("审计日志落库失败（action=%s）", entry.get("action"), exc_info=True)


def _extract_user_id(request: Request) -> int | None:
    """从 Authorization JWT 解 user_id（匿名/无效 → None）。"""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from app.domain.auth.security import decode_token

        payload = decode_token(auth[7:].strip())
        return int(payload["sub"])
    except Exception:  # noqa: BLE001 - 匿名/无效凭证：审计记 NULL
        return None


class AuditMiddleware(BaseHTTPMiddleware):
    """写操作审计中间件：白名单匹配 → 响应后台任务落库。"""

    def __init__(self, app: Any, api_prefix: str = "/api"):
        super().__init__(app)
        self._prefix = api_prefix.rstrip("/")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        method = request.method.upper()
        raw_path = request.url.path
        rel = raw_path[len(self._prefix):] if raw_path.startswith(self._prefix) else raw_path
        matched = match_audit_rule(method, rel)
        if matched is None:
            return await call_next(request)

        # JSON body 必须在 call_next 之前读（缓存后下游可重读；call_next 之后
        # receive 流已结束，读到的是空）。multipart（文件上传）不读，避免缓冲大文件。
        body: bytes | None = None
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.body()
            except Exception:  # noqa: BLE001
                body = None

        response = await call_next(request)
        status = response.status_code

        action, rtype, rid = matched
        is_login = action == "user.login"
        # 只记成功操作；登录例外（失败也是安全审计信号）
        if status < 200 or status >= 300:
            if not (is_login and status in (400, 401, 403)):
                return response

        detail: dict = {"status": status}
        summary = _summarize_body(body)
        if summary:
            detail["body"] = summary
        username = (summary or {}).get("username")
        if isinstance(username, str):
            detail["username"] = username

        # create 类动作路径上没有 id：从响应体 data.id 捕获 resource_id
        if rid is None and 200 <= status < 300:
            rid, response = await _capture_response_resource_id(response)

        entry = {
            "user_id": _extract_user_id(request),
            "username": username if is_login else None,
            "action": action,
            "resource_type": rtype,
            "resource_id": rid,
            "method": method,
            "path": raw_path,
            "status_code": status,
            "detail": detail,
        }
        task = BackgroundTask(write_audit_log, entry)
        if response.background is None:
            response.background = task
        # 已有 background 的场景当前不存在；保留主任务优先
        return response


async def _capture_response_resource_id(response: Response) -> tuple[str | None, Response]:
    """从 2xx 响应体捕获创建资源 id（data.id）→ (rid, 重建后的响应)。

    BaseHTTPMiddleware 的响应是流式的：读完 body_iterator 后需用缓存内容
    重建响应（头部原样保留，Content-Length 由新 Response 重算）。
    任何异常都不影响响应本身（返回原响应、rid=None）。
    """
    try:
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        raw = b"".join(chunks)
    except Exception:  # noqa: BLE001
        return None, response

    rid: str | None = None
    try:
        obj = json.loads(raw)
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and data.get("id") is not None:
            rid = str(data["id"])
    except Exception:  # noqa: BLE001 - 非 JSON 响应：不强取
        pass

    rebuilt = Response(
        content=raw,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        media_type=response.media_type,
    )
    rebuilt.background = response.background
    return rid, rebuilt


def register_audit_middleware(application: Any, api_prefix: str = "/api") -> None:
    """应用工厂调用：注册审计中间件（放在路由之后无影响，中间件按洋葱模型包裹全部路由）。"""
    application.add_middleware(AuditMiddleware, api_prefix=api_prefix)
    logger.info("审计中间件已注册（写操作白名单 %d 条）", len(_AUDIT_RULES))
