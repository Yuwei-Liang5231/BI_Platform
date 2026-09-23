"""进程内每日定时器（P5/A1）：零新增依赖的轻量调度。

设计：
- FastAPI lifespan 启动一个 asyncio 后台任务，每 30 分钟检查一次；
- 当本地时间到达 `ai_insight_hour` 点且今日未跑 → 在线程池中执行
  `run_daily_insight`（同步阻塞的 LLM/DB 调用不卡事件循环）；
- 当日幂等（内存 last_run_date + 洞察表去重双保险）；启动时若已过
  配置时刻且今日未跑则补跑一次（进程重启不丢当日洞察）；
- 失败只记日志，绝不影响主服务。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.infra.scheduler")

_CHECK_INTERVAL_SECONDS = 30 * 60
_last_run_date: date | None = None


def _should_run_now(now: datetime, settings) -> bool:
    global _last_run_date
    if not settings.ai_insight_enabled:
        return False
    if _last_run_date == now.date():
        return False
    return now.hour >= settings.ai_insight_hour


async def _tick() -> None:
    global _last_run_date
    settings = get_settings()
    if not _should_run_now(datetime.now(), settings):
        return
    _last_run_date = date.today()
    logger.info("scheduler: 触发每日洞察任务（hour=%s）", settings.ai_insight_hour)
    try:
        await asyncio.to_thread(_run_insight_sync)
    except Exception:  # noqa: BLE001 - 定时任务失败只记日志，不影响主服务
        logger.exception("scheduler: 每日洞察任务执行失败")


def _run_insight_sync() -> dict:
    from app.infra.database import get_db
    from app.domain.insight.service import run_daily_insight

    db = next(get_db())
    try:
        return run_daily_insight(db)
    finally:
        db.close()


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: 巡检循环异常")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> asyncio.Task | None:
    """启动每日洞察后台循环（lifespan 调用；禁用时返回 None）。"""
    settings = get_settings()
    if not settings.ai_insight_enabled:
        logger.info("scheduler: 每日洞察未启用（ai_insight_enabled=false）")
        return None
    logger.info("scheduler: 每日洞察已启动（每日 %s 点后自动执行）", settings.ai_insight_hour)
    return asyncio.create_task(_loop())
