"""问数多轮会话上下文（B9.2-4）。

设计约束：
- 仅存**意图**（指标/维度/筛选/排序/时间），数值每轮仍由 compute 单点出口现算——
  红线 1/2 不变；不落库（内存 + TTL），重启即失，会话过期降级为单轮。
- session_id 由后端生成（secrets），首问不带时新建并随理解卡返回，前端每轮携带。
- 权限不存会话：每轮以当轮登录用户重校验（继承指标若已受限 → 提示无权限）。
"""

from __future__ import annotations

import secrets
import threading
import time

SESSION_TTL_SECONDS = 30 * 60       # 会话存活：30 分钟（无新请求即过期）
SESSION_MAX = 1000                  # 上限保护：超限淘汰最旧会话

# 会话中暂存的意图字段（白名单：缺省字段不继承）
_INTENT_FIELDS = (
    "metric_code", "dimension", "filters", "order_by", "order", "top_n",
    "compare", "start", "end",
)

_lock = threading.Lock()
_sessions: dict[str, dict] = {}     # session_id -> {"intent": {...}, "ts": float}


def new_session_id() -> str:
    return secrets.token_urlsafe(16)


def get_intent(session_id: str | None) -> dict | None:
    """取上一轮意图；session 无效/过期返回 None（调用方按首问处理）。"""
    if not session_id:
        return None
    with _lock:
        sess = _sessions.get(session_id)
        if sess is None or time.monotonic() - sess["ts"] > SESSION_TTL_SECONDS:
            _sessions.pop(session_id, None)  # 惰性过期
            return None
        sess["ts"] = time.monotonic()    # 活跃会话续期
        return {k: sess["intent"][k] for k in _INTENT_FIELDS if k in sess["intent"]}


def save_intent(session_id: str, intent: dict) -> None:
    """保存/覆盖会话意图（每轮理解卡生成后调用）。"""
    clean = {k: intent[k] for k in _INTENT_FIELDS if intent.get(k) is not None}
    with _lock:
        if len(_sessions) >= SESSION_MAX and session_id not in _sessions:
            oldest = min(_sessions, key=lambda s: _sessions[s]["ts"])
            _sessions.pop(oldest, None)
        _sessions[session_id] = {"intent": clean, "ts": time.monotonic()}


def clear_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)
