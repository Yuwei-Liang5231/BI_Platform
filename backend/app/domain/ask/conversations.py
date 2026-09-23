"""问数会话持久化（B9.2-6）。

职责边界：
- 只持久化**消息记录**（问句原文 + 理解卡快照 + 结果快照），供历史列表、
  恢复查看与删除；结果快照是当时算出的真实值留档，恢复不重算。
- 追问的**意图继承**仍走内存 session（TTL 30 分钟，键 = 会话 id），
  恢复超过 TTL 的旧会话续聊时按首问处理——红线（AI 不产数值）不变。
- 会话归属严格按 user_id 隔离：他人会话一律按不存在处理（不泄露存在性）。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.infra.models import AskConversation, AskMessage
from app.core.response import BusinessError

TITLE_MAX = 24          # 标题取首问前 24 字
CONV_LIST_LIMIT = 50    # 历史列表上限（最近使用优先）


def _owned_conversation(db: Session, user, conversation_id: int) -> AskConversation:
    conv = db.get(AskConversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise BusinessError("会话不存在", 40400)
    return conv


def list_conversations(db: Session, user, project_id: int | None = None) -> list[dict]:
    """当前用户的历史会话（按最近使用排序，限 50 条）；project_id 指定时仅该项目。"""
    query = db.query(AskConversation).filter(AskConversation.user_id == user.id)
    if project_id is not None:
        query = query.filter(AskConversation.project_id == project_id)
    rows = query.order_by(AskConversation.updated_at.desc()).limit(CONV_LIST_LIMIT).all()
    return [
        {
            "id": c.id,
            "title": c.title or "（未命名对话）",
            "project_id": c.project_id,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in rows
    ]


def get_or_create_conversation(
    db: Session, user, conversation_id: int | None, project_id: int | None = None
) -> AskConversation:
    """首问无会话则新建（B9.3：挂当前项目）；带会话 id 则校验归属。

    2026-09-23 修订：新建会话必须**立即 commit**（此前 flush 挂起写事务直到
    请求结束）——build_card 随后要调 LLM（30~60s）并在中途走 record_llm_call
    旁路落库，SQLite 单写者：主连接的未提交写事务会让观测旁路 INSERT
    「database is locked」被静默吞掉（问数调用记录全部丢失的根因，实测复现）。
    """
    if conversation_id is None:
        conv = AskConversation(user_id=user.id, project_id=project_id)
        db.add(conv)
        db.commit()  # 取 id + 结束写事务（LLM 调用期间不得持有写锁）
        db.refresh(conv)
        return conv
    return _owned_conversation(db, user, conversation_id)


def append_turn(
    db: Session,
    conv: AskConversation,
    question: str,
    card: dict | None,
) -> None:
    """落一轮对话：用户问句 + assistant 理解卡快照；首问生成标题。"""
    db.add(AskMessage(conversation_id=conv.id, role="user", question=question))
    db.add(
        AskMessage(
            conversation_id=conv.id,
            role="assistant",
            question=question,
            card_json=json.dumps(card or {}, ensure_ascii=False, default=str),
        )
    )
    if not conv.title:
        conv.title = question[:TITLE_MAX]
    from app.infra.models import local_now

    conv.updated_at = local_now()  # 显式刷新（无字段变化时 onupdate 不触发）
    db.commit()


def attach_results(db: Session, user, conversation_id: int, results: list[dict]) -> None:
    """执行成功后把结果快照回写到该会话**最新一条** assistant 消息。"""
    conv = _owned_conversation(db, user, conversation_id)
    msg = (
        db.query(AskMessage)
        .filter(AskMessage.conversation_id == conv.id, AskMessage.role == "assistant")
        .order_by(AskMessage.id.desc())
        .first()
    )
    if msg is None:
        return  # 会话无理解卡轮次（异常调用），静默忽略
    msg.results_json = json.dumps(results, ensure_ascii=False, default=str)
    from app.infra.models import local_now

    conv.updated_at = local_now()
    db.commit()


def get_messages(db: Session, user, conversation_id: int) -> list[dict]:
    """恢复会话：按序返回消息（user 问句 / assistant 卡+结果快照）。"""
    conv = _owned_conversation(db, user, conversation_id)
    rows = (
        db.query(AskMessage)
        .filter(AskMessage.conversation_id == conv.id)
        .order_by(AskMessage.id.asc())
        .all()
    )
    out: list[dict] = []
    for m in rows:
        if m.role == "user":
            out.append({"role": "user", "text": m.question})
        else:
            try:
                card = json.loads(m.card_json or "{}")
            except json.JSONDecodeError:
                card = {}
            try:
                results = json.loads(m.results_json or "[]")
            except json.JSONDecodeError:
                results = []
            out.append(
                {
                    "role": "assistant",
                    "question": m.question,
                    "card": card,
                    "results": results,
                    "readonly": True,  # 恢复的历史轮一律只读
                }
            )
    return out


def rename_conversation(db: Session, user, conversation_id: int, title: str) -> None:
    """重命名会话（B9.2-6）：标题截断至 TITLE_MAX，空标题拒绝。"""
    conv = _owned_conversation(db, user, conversation_id)
    clean = (title or "").strip()
    if not clean:
        raise BusinessError("标题不能为空", 40000)
    conv.title = clean[:TITLE_MAX]
    db.commit()


def delete_conversation(db: Session, user, conversation_id: int) -> None:
    """删除会话及其消息（物理删除；历史快照无审计价值）。"""
    conv = _owned_conversation(db, user, conversation_id)
    db.query(AskMessage).filter(AskMessage.conversation_id == conv.id).delete()
    db.delete(conv)
    db.commit()
