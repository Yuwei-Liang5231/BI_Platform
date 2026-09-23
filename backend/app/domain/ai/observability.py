"""AI 调用观测（B2）：LLM 调用落库 + 聚合查询。

原则：
- **零阻塞**：观测落库走独立会话（fresh_session），任何失败只记日志，
  绝不影响 AI 主流程（与反馈闭环同一规矩）；
- **只记真实请求**：LLM 未配置时 chat_json 直接返回、不发请求（meta 无
  attempted 标记），不产生观测记录——面板反映的是「真实 LLM 用量与质量」；
- outcome 语义：ok（成功产出）/ llm_failed（请求失败或返回非法结构）/
  audit_filtered（返回内容被三道审计全剔）。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.domain.ai.observability")


def record_llm_call(
    kind: str,
    meta: dict[str, Any] | None,
    *,
    outcome: str | None = None,
    project_id: int | None = None,
) -> None:
    """落一条 LLM 调用观测（尽力而为）。

    meta 为 infra.llm.chat_json 回填的元数据 dict；outcome 缺省按 meta.ok
    推断，调用方可显式覆盖（如 audit_filtered）；project_id 为调用所属项目
    （无上下文传 None，按「NULL=默认项目」约定归默认项目视图）。
    """
    if not meta or not meta.get("attempted"):
        return  # 未真正发起请求（LLM 未配置/缺配置）：不记
    try:
        from app.infra.database import fresh_session
        from app.infra.models import AiCallLog

        row = AiCallLog(
            kind=(kind or "unknown")[:40],
            outcome=(outcome or ("ok" if meta.get("ok") else "llm_failed"))[:16],
            duration_ms=meta.get("duration_ms"),
            model=(meta.get("model") or "")[:100] or None,
            prompt_tokens=meta.get("prompt_tokens"),
            completion_tokens=meta.get("completion_tokens"),
            project_id=project_id,
        )
        session = fresh_session()
        try:
            session.add(row)
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 观测零阻塞：失败只记日志
        logger.warning("AI 调用观测落库失败（kind=%s）", kind, exc_info=True)
