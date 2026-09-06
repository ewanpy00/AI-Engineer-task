"""Часовой планировщик (T-23, ADR-2).

APScheduler внутри того же процесса: отдельного воркера, брокера и beat'а на
~100 HTTP-вызовов в час не нужно. Планировщик живёт и умирает вместе с
веб-процессом — рестарт посреди захода теряет незавершённую работу, это
осознанная плата (ADR-2).

Дублей между плановым и ручным запуском планировщик не предотвращает: это
работа advisory-локи в `IngestRunner.run`, которая держится и при двух
инстансах приложения.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ingest.runner import IngestRunner

log = logging.getLogger(__name__)

JOB_ID = "ingest-hourly"
MISFIRE_GRACE_S = 600  # проспали больше десяти минут — час потерян, догонять нечего


async def _hourly(runner: IngestRunner) -> None:
    """Обёртка над заходом: исключение из джобы не должно ронять планировщик."""
    try:
        await runner.run("schedule")
    except Exception:  # noqa: BLE001 — job'а без обработчика убьёт только себя
        log.exception("плановый заход упал")


def create_scheduler(runner: IngestRunner) -> AsyncIOScheduler:
    """Cron `0 * * * *` в UTC, без наложения заходов друг на друга."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _hourly,
        CronTrigger(hour="*", minute=0, timezone="UTC"),
        args=[runner],
        id=JOB_ID,
        max_instances=1,   # заход длиннее часа не порождает второй
        coalesce=True,     # проспали несколько запусков — выполняем один
        misfire_grace_time=MISFIRE_GRACE_S,
        replace_existing=True,
    )
    return scheduler
